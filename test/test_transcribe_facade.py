"""``kiro_crew.transcribe`` keeps the patch seams and the surface the one module had.

``kiro_crew.transcribe`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over
``transcribe_ffmpeg``, ``transcribe_aws`` and ``transcribe_local``. Callers, tests and
documents still reach every name as ``kiro_crew.transcribe.<name>``, and the moved code
reads its names as plain module globals, so a patch through the facade reaches that code
only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirror facade to the shared contract.
These tests pin what is particular to this one: every name the test trees patch through
it reaches every module holding it; the owners are exactly the ``transcribe_*.py``
siblings, each importing only the owners before it; the family logs through one logger
named for the facade; an exception class an owner defines still reports the facade's
module; and every project name the facade imported before its owners moved out stays
bound on it, so a reader still type-checks through the facade path.
"""

from __future__ import annotations

import ast
import enum
import importlib
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, transcribe

pytestmark = pytest.mark.xdist_group(name="tree_scan_transcribe_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams",)

FACADE = "kiro_crew.transcribe"
SRC_DIR = Path(transcribe.__file__).resolve().parent

#: Names the test trees are known to patch through the facade, one per kind of seam:
#: a function several owners call, an owner's mutable state, a constant computed at
#: import, an optional import's fallback binding, a stdlib module, and names the facade
#: keeps. The scan must find all of them, so a scanner that stops matching goes red
#: instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_open_ffmpeg_for_execution",
        "_resolve_ffmpeg_for_execution",
        "_create_ffmpeg_subprocess",
        "_ffmpeg_snapshot_roots_cleaned",
        "_FFMPEG_CANDIDATE_DIRS",
        "boto3",
        "_load_aws_transcribe_components",
        "_make_temp_wav",
        "shutil",
        "transcribe_audio",
        "_detect_silence_ends",
    }
)

#: The project names the facade imported before the split, with their home modules. A
#: reader that spells one through ``kiro_crew.transcribe`` keeps type-checking only while
#: the facade binds it itself.
_BASE_PROJECT_IMPORTS = {
    "aws_consent": "kiro_crew",
    "decoder": "kiro_crew.stt",
    "filter_hallucinations": "kiro_crew.stt.hallucinations",
    "install_hint": "kiro_crew.extras",
    "pinned_fs": "kiro_crew",
    "platform_compat": "kiro_crew",
    "stt": "kiro_crew",
}


def _index() -> _facade_mirror.MirrorIndex:
    return transcribe.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [transcribe, *(sys.modules[name] for name in _index().owners)]


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    found = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/"):
            found.append(path)
        elif rel.startswith("src/kiro_crew/apps/builtins/") and (
            "/tests/" in rel or path.name.startswith("test_") or path.name == "conftest.py"
        ):
            found.append(path)
    return found


def _spellings() -> dict[str, str]:
    """The facade's own name and every src module's module-level binding of it."""
    spellings = {FACADE: FACADE}
    for path, _text, tree in source_corpus.parsed_candidates(require_any=("transcribe",)):
        rel = path.relative_to(source_corpus.src_root()).with_suffix("")
        module = ".".join(("kiro_crew", *rel.parts)).removesuffix(".__init__")
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
                for alias in node.names:
                    if alias.name == "transcribe" and module != FACADE:
                        spellings[f"{module}.{alias.asname or alias.name}"] = FACADE
    return spellings


def _corpus_patched_names() -> set[str]:
    spellings = _spellings()
    names: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not facade_mirror_scan._mentions(text, spellings):
            continue
        names |= facade_mirror_scan.text_patched_names(text, spellings).get(FACADE, set())
        if facade_mirror_scan.needs_syntax_tree(text, spellings):
            try:
                events = facade_mirror_scan.patch_events(text, spellings)
            except SyntaxError:
                continue
            names |= facade_mirror_scan.names_by_target(events, {FACADE}).get(FACADE, set())
    return names


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the one module had: a write to ``transcribe.<name>`` was what every
    reader of the name read. Every member of the family that binds a patched name sees
    the patched object, and the undo leaves the family one object per name again."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(transcribe, name):
            continue  # a test that creates the name with raising=False
        holders = [module for module in family if name in vars(module)]
        sentinel = object()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(transcribe, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(transcribe, name) is not sentinel:
                stale.append(FACADE)
        checked += 1
        if stale:
            missed[name] = stale
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    assert _index().divergent() == ()


def test_the_owners_are_the_transcribe_siblings_in_layer_order() -> None:
    """``_OWNERS`` names every ``transcribe_*.py`` sibling, and each owner imports only
    owners listed before it: the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    on_disk = {f"kiro_crew.{p.stem}": p for p in SRC_DIR.glob("transcribe_*.py")}
    assert sorted(owners) == sorted(on_disk)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(on_disk[owner].read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``transcribe.py`` was a plain module: the facade forwards no legacy table and keeps
    the default ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    """``caplog`` filters keyed on ``kiro_crew.transcribe`` keep seeing the moved code."""
    assert transcribe.logger.name == FACADE
    loggers = [m for m in _family() if "logger" in vars(m)]
    assert len(loggers) >= 3
    assert [m.__name__ for m in loggers if vars(m)["logger"] is not transcribe.logger] == []


def test_an_exception_an_owner_defines_keeps_the_facade_module() -> None:
    """A traceback or a log line renders a raised class as ``module.qualname``, so an
    exception or enum class an owner defines reports ``kiro_crew.transcribe``, as it
    did before it moved."""
    found = {
        name: value
        for owner in _index().owners
        for name, value in vars(sys.modules[owner]).items()
        if isinstance(value, type)
        and issubclass(value, (BaseException, enum.Enum))
        and name
        in _facade_mirror._defined_names(
            ast.parse(Path(sys.modules[owner].__file__ or "").read_text(encoding="utf-8"))
        )
    }
    assert "VocabularyListError" in found, "no owner-defined exception was found"
    assert {name: cls.__module__ for name, cls in found.items()} == dict.fromkeys(found, FACADE)


def _static_bindings(path: Path) -> set[str]:
    """Names a module binds at import time with a module-level ``import``, outside
    ``if TYPE_CHECKING:`` -- the bindings a type checker reads off the module."""
    found: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return found


def _home(value: object) -> str:
    if isinstance(value, ModuleType):
        return value.__name__
    return getattr(value, "__module__", "") or ""


def test_every_project_name_the_facade_imported_stays_bound_on_it() -> None:
    """A name the facade imported from the rest of ``kiro_crew`` is bound on it still,
    statically and as its home module's object; and no name the mirror serves comes
    from a project module outside the family, which would be visible to a type checker
    only through a ``TYPE_CHECKING`` import."""
    bound = _static_bindings(Path(transcribe.__file__))
    assert "decoder" in bound, "the static reader went blind"
    missing = sorted(name for name in _BASE_PROJECT_IMPORTS if name not in bound)
    assert missing == []
    for name, home in _BASE_PROJECT_IMPORTS.items():
        assert vars(transcribe)[name] is getattr(importlib.import_module(home), name), name
    family = {FACADE, *_index().owners}
    defined = {
        name
        for owner in _index().owners
        for name in _facade_mirror._defined_names(
            ast.parse(Path(sys.modules[owner].__file__ or "").read_text(encoding="utf-8"))
        )
    }
    imported_by_owners = sorted(set(_index().exports) - defined)
    assert imported_by_owners, "no served name is an owner's import, so this case measured nothing"
    served_from_outside = {
        name: _home(getattr(transcribe, name))
        for name in imported_by_owners
        if _home(getattr(transcribe, name)).startswith("kiro_crew")
        and _home(getattr(transcribe, name)) not in family
    }
    assert served_from_outside == {}
